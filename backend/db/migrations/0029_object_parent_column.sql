-- 2.11.0: an object's image is a column, samples.parent_id, kept by triggers from metadata.parent_id (which stays
-- what clients write and read): the id metadata.parent_id names when that sample is a non-object of the same dataset,
-- else NULL. Deleting an image deletes its objects from 2.11.0, so the objects whose image is already gone (no sample
-- has that id any more) are deleted here, and the upgrade says how many, per dataset.
--
-- Like 0021 it fills a large table itself, with the app stopped (CONTRIBUTING, Database migrations, rule 3): filled
-- by a job while the app ran, the column would be half there, and every reader would need two versions until it
-- finished. So it refuses to run while anything else is connected, and checks the disk first. It runs in one
-- transaction: if it fails or is interrupted, nothing has changed and it can run again. Its progress lines start with
-- "0029:" (apply_migrations.sh shows them as they come). Afterwards, run VACUUM (ANALYZE) samples once the app is up:
-- each linked object leaves a dead row version.
SET LOCAL lock_timeout = '2s';

-- 1. Nothing else may be connected: the migration holds samples until it commits, so a running backend would hang
-- on every request. (A health check's pg_isready shows up with no user.)
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
        RAISE EXCEPTION '0029 gives every object sample its image in a new column, so stop the app first. Still connected: %', others
            USING HINT = 'Stop every service but the database (backend, job runner, workers, backups), then run '
                         'apply_migrations.sh again.';
    END IF;
END $$;

-- 2. An array field already called parent_id is in the way, refused with its name.
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_attribute
                WHERE attrelid = 'samples'::regclass AND attname = 'parent_id' AND NOT attisdropped) THEN
        RAISE EXCEPTION 'samples already has a column named parent_id (an array field): this release needs the name for an object''s image. Copy that field to another name and delete it, then upgrade';
    END IF;
END $$;

-- 3. A catalog change only, no rewrite. It holds samples (ACCESS EXCLUSIVE) until the commit, so a client that
-- connects later (a backup's pg_dump, say) waits for the migration instead of taking a lock it needs.
ALTER TABLE samples ADD COLUMN parent_id INTEGER;

-- 4. Room for a new version of every object row (the heap only: an unchanged metadata keeps its TOAST) with its
-- entries in samples' indexes, the new index (about 16 bytes a row), the WAL written meanwhile (as much again, until
-- it levels off at max_wal_size, which is soft), and headroom: a quarter of it, at least as much again up to 1 GB.
-- Where the server can't run df (no superuser, a managed database), the check is skipped, as the app's own is.
DO $$
DECLARE
    rows_now BIGINT := (SELECT reltuples FROM pg_class WHERE oid = 'samples'::regclass)::BIGINT;
    objects BIGINT := (SELECT count(*) FROM samples WHERE media_type = 'object');
    written BIGINT;
    needed BIGINT;
    free BIGINT;
    data_dir TEXT;
BEGIN
    IF rows_now < 1 THEN  -- never analyzed
        rows_now := (SELECT count(*) FROM samples);
    END IF;
    written := rows_now * 16;
    IF rows_now > 0 THEN
        written := written
            + (objects::NUMERIC * (pg_relation_size('samples') + pg_indexes_size('samples')) / rows_now)::BIGINT;
    END IF;
    needed := written
        + least(written, pg_size_bytes(current_setting('max_wal_size')))
        + greatest(written / 4, least(greatest(written, pg_size_bytes('64MB')), pg_size_bytes('1GB')));
    BEGIN
        data_dir := current_setting('data_directory');
        IF position('''' IN data_dir) > 0 THEN
            RAISE NOTICE '0029: skipped the free-space check (the data directory''s name has a quote)';
            RETURN;
        END IF;
        CREATE TEMP TABLE _fridge_df (line TEXT) ON COMMIT DROP;
        EXECUTE format('COPY _fridge_df FROM PROGRAM %L', 'df -B1 --output=avail ''' || data_dir || ''' | tail -n 1');
        SELECT trim(line)::BIGINT INTO free FROM _fridge_df LIMIT 1;
    EXCEPTION WHEN OTHERS THEN
        RAISE NOTICE '0029: skipped the free-space check: %', SQLERRM;
        RETURN;
    END;
    IF free < needed THEN
        RAISE EXCEPTION '0029 needs about % free on the database''s disk while it links % objects to their images; % is free',
            pg_size_pretty(needed), objects, pg_size_pretty(free)
            USING HINT = 'Free some space, or move the database to a larger disk, then run apply_migrations.sh again.';
    END IF;
    RAISE NOTICE '0029: % free on the database''s disk, about % needed', pg_size_pretty(free), pg_size_pretty(needed);
END $$;

-- Links the objects of one dataset whose image is there and whose column doesn't name it yet; returns how many. The
-- CASE keeps the cast from ever seeing a value that isn't a number. only_unlinked: just the objects with no image (by
-- idx_samples_unlinked_objects), as the orphan finder links them before it counts or deletes any; a linked object
-- needs no re-check there, as the parent key (0030) keeps its image and the triggers re-derive the column on every
-- change of the object or its image. It sets plan_cache_mode = force_custom_plan for its own statement: only under a
-- custom plan does `NOT only_unlinked OR o.parent_id IS NULL` fold to idx_samples_unlinked_objects' predicate (a
-- generic plan, which a function's cached statement may switch to, keeps the OR and visits every object of the
-- dataset). The setting chooses which cached plan runs, never a plan's shape, and costs one setting save per call,
-- which is one per dataset.
CREATE OR REPLACE FUNCTION link_objects(target_dataset INTEGER, only_unlinked BOOLEAN) RETURNS BIGINT
SET plan_cache_mode = force_custom_plan AS $$
DECLARE
    linked BIGINT;
BEGIN
    UPDATE samples o SET parent_id = p.id
      FROM samples p
     WHERE o.dataset_id = target_dataset AND o.media_type = 'object' AND p.dataset_id = target_dataset
       AND (NOT only_unlinked OR o.parent_id IS NULL)
       AND p.id = CASE WHEN o.metadata->>'parent_id' ~ '^[0-9]{1,10}$' THEN (o.metadata->>'parent_id')::bigint END
       AND p.media_type <> 'object' AND o.parent_id IS DISTINCT FROM p.id;
    GET DIAGNOSTICS linked = ROW_COUNT;
    RETURN linked;
END;
$$ LANGUAGE plpgsql;

-- Every object of one dataset, as this migration links them and a revert relinks them (rows it restores can carry a
-- stale column).
CREATE OR REPLACE FUNCTION link_object_parents(target_dataset INTEGER) RETURNS BIGINT AS $$
BEGIN
    RETURN link_objects(target_dataset, FALSE);
END;
$$ LANGUAGE plpgsql;

-- 5. The column filled, dataset by dataset, with the samples triggers off, on purpose (CONTRIBUTING, rule 4): a
-- filled column is nothing new to anyone. It isn't sent to followers (each fills its own here, from its mirrors'
-- translated metadata) and isn't a statistics field; on a follower the mirror guard would refuse the statement. So it
-- writes no change-log rows, statistics or duplicate work. A dataset being deleted is left alone: its rows are going.
ALTER TABLE samples DISABLE TRIGGER USER;
DO $$
DECLARE
    dataset RECORD;
    linked BIGINT;
    done BIGINT := 0;
    total BIGINT;
    began TIMESTAMPTZ;
BEGIN
    CREATE TEMP TABLE _fridge_objects ON COMMIT DROP AS
        SELECT dataset_id, count(*) AS objects FROM samples WHERE media_type = 'object' GROUP BY dataset_id;
    SELECT coalesce(sum(o.objects), 0) INTO total
      FROM _fridge_objects o JOIN datasets d ON d.id = o.dataset_id WHERE NOT d.deleting;
    -- Measured on copies on an NVMe disk, PostgreSQL 18 with its default settings: 500,000 objects linked in 24 s
    -- among 1.1 million samples, and 3.2 million in 240 s among 4 million, so about 13,000 a second on a large table.
    -- On the larger copy the rest took 11 s: the orphan pass 0.6 s, the new indexes 2.0 s and 0.5 s, rebuilding
    -- idx_samples_parent_id 7.2 s (step 8, about 450,000 objects a second) and ANALYZE 0.4 s; finding and deleting
    -- 5,000 orphans among 505,000 objects took 1.2 s.
    IF total > 0 THEN
        RAISE NOTICE '0029: linking % objects to their images and indexing them again: about % min, the app stopped meanwhile',
            total, greatest(1, ceil((total / 13000.0 + total / 450000.0) / 60));
    END IF;
    FOR dataset IN SELECT d.id, d.name, d.deleting, o.objects
                     FROM datasets d JOIN _fridge_objects o ON o.dataset_id = d.id ORDER BY d.id LOOP
        IF dataset.deleting THEN
            RAISE NOTICE '0029: left dataset % (%) alone: it is being deleted', dataset.id, dataset.name;
            CONTINUE;
        END IF;
        began := clock_timestamp();
        linked := link_object_parents(dataset.id);
        done := done + dataset.objects;
        RAISE NOTICE '0029: linked % of the % objects of dataset % (%) to their images in % s; % %% done', linked,
            dataset.objects, dataset.id, dataset.name, round(extract(epoch FROM clock_timestamp() - began)::numeric, 1),
            least(100, round(100.0 * done / total));
    END LOOP;
END $$;
ALTER TABLE samples ENABLE TRIGGER USER;

-- 6. The objects whose image is already gone are deleted: from 2.11.0 deleting an image deletes its objects, so none
-- is left without one. Such an orphan is an object whose metadata.parent_id is a sample id (the column's own test)
-- that no sample has, in any dataset. The triggers are on again for it, so it is a delete like any other: a followed
-- dataset's followers fetch the changed blocks, statistics recount them, and the cascades take the objects' vectors,
-- map points, hashes and scores. The field caches are kept as a bulk delete keeps them (field_registry.py:
-- invalidate_values, then prune_fields). Kept, with no image, and counted: objects that name another object, or a
-- sample of another dataset (mis-pointed, not orphaned: a delete here could only be undone from a backup), whose
-- parent_id isn't a sample id, or that have none. A mirror's objects aren't deleted: a mirror changes only through
-- its sync (its guard would refuse), so they stay, counted, until their primary deletes them.
DO $$
DECLARE
    dataset RECORD;
    orphans INTEGER[];
    on_objects BIGINT;
    elsewhere BIGINT;
    unusable BIGINT;
    unnamed BIGINT;
    deleted BIGINT;
    deleted_total BIGINT := 0;
    datasets_deleted_in INTEGER := 0;
    names TEXT[];
    labels TEXT[];
    affected TEXT[];
    unused TEXT[];
BEGIN
    FOR dataset IN SELECT d.id, d.name, EXISTS (SELECT 1 FROM mirrors m WHERE m.dataset_id = d.id) AS mirror
                     FROM datasets d WHERE NOT d.deleting ORDER BY d.id LOOP
        SELECT coalesce(array_agg(c.id ORDER BY c.id) FILTER (WHERE c.kind = 'orphan'), '{}'),
               count(*) FILTER (WHERE c.kind = 'object'), count(*) FILTER (WHERE c.kind = 'elsewhere'),
               count(*) FILTER (WHERE c.kind = 'unusable'), count(*) FILTER (WHERE c.kind = 'unnamed')
          INTO orphans, on_objects, elsewhere, unusable, unnamed
          FROM (SELECT o.id,
                       CASE WHEN coalesce(o.metadata->'parent_id', 'null') = 'null' THEN 'unnamed'
                            WHEN NOT coalesce(o.metadata->>'parent_id' ~ '^[0-9]{1,10}$', FALSE) THEN 'unusable'
                            WHEN EXISTS (SELECT 1 FROM samples p WHERE p.dataset_id = o.dataset_id
                                            AND p.id = (o.metadata->>'parent_id')::bigint) THEN 'object'
                            WHEN EXISTS (SELECT 1 FROM samples p
                                          WHERE p.id = (o.metadata->>'parent_id')::bigint) THEN 'elsewhere'
                            ELSE 'orphan' END AS kind
                  FROM samples o
                 WHERE o.dataset_id = dataset.id AND o.media_type = 'object' AND o.parent_id IS NULL) c;
        IF dataset.mirror THEN
            IF cardinality(orphans) + on_objects + elsewhere + unusable + unnamed > 0 THEN
                RAISE NOTICE '0029: dataset % (%), a mirror: kept % objects whose image is gone, until its primary deletes them (a mirror changes only through its sync); kept, with no image: % naming another object, % naming a sample of another dataset, % with a parent_id that is not a sample id, % with no parent_id',
                    dataset.id, dataset.name, cardinality(orphans), on_objects, elsewhere, unusable, unnamed;
            END IF;
            CONTINUE;
        END IF;
        deleted := 0;
        IF cardinality(orphans) > 0 THEN
            WITH removed AS (
                DELETE FROM samples WHERE dataset_id = dataset.id AND id = ANY(orphans) RETURNING metadata
            )
            SELECT count(*),
                   ARRAY(SELECT DISTINCT k FROM removed r, jsonb_object_keys(
                             CASE WHEN jsonb_typeof(r.metadata) = 'object' THEN r.metadata ELSE '{}' END) k)
              INTO deleted, names
              FROM removed;
            deleted_total := deleted_total + deleted;
            datasets_deleted_in := datasets_deleted_in + 1;
            -- invalidate_values: the deleted rows' fields and tags, with their label lists, lose their complete
            -- value lists, and a defined field without a cache row gets an empty one.
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
            -- prune_fields: a plain metadata field that no sample of the dataset has any more is unregistered.
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
        END IF;
        IF deleted + on_objects + elsewhere + unusable + unnamed > 0 THEN
            RAISE NOTICE '0029: dataset % (%): deleted % objects whose image is gone (no sample has their parent_id any more); kept, with no image: % naming another object, % naming a sample of another dataset, % with a parent_id that is not a sample id, % with no parent_id',
                dataset.id, dataset.name, deleted, on_objects, elsewhere, unusable, unnamed;
        END IF;
    END LOOP;
    IF datasets_deleted_in > 1 THEN
        RAISE NOTICE '0029: deleted % objects whose image was gone in all', deleted_total;
    END IF;
END $$;

-- 7. Built after the column is filled and the orphans are gone, so the fill doesn't write into them: the column's
-- index, and the objects still without an image (by dataset; nearly empty), which are counted and listed by it.
CREATE INDEX idx_samples_dataset_parent ON samples (dataset_id, parent_id);
CREATE INDEX idx_samples_unlinked_objects ON samples (dataset_id) WHERE media_type = 'object' AND parent_id IS NULL;

-- 8. The index the triggers below find an image's waiting objects by. Until now it held metadata.parent_id's exact
-- text, so "0012" never matched image 12 although the object's own trigger reads it as 12. It holds the digits with
-- leading zeros trimmed now, read the same way on both sides; an id is never 0, so a value of zeros trims to '' and
-- matches nothing. Same name, rebuilt.
DROP INDEX IF EXISTS idx_samples_parent_id;
CREATE INDEX idx_samples_parent_id ON samples ((ltrim(metadata->>'parent_id', '0'))) WHERE media_type = 'object';

-- 9. The triggers that keep the column, as schema.sql makes them.
CREATE OR REPLACE FUNCTION samples_set_parent() RETURNS trigger AS $$
BEGIN
    NEW.parent_id := NULL;
    IF NEW.media_type = 'object' AND NEW.metadata->>'parent_id' ~ '^[0-9]{1,10}$' THEN
        SELECT p.id INTO NEW.parent_id FROM samples p
         WHERE p.dataset_id = NEW.dataset_id AND p.id = (NEW.metadata->>'parent_id')::bigint
           AND p.media_type <> 'object';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

-- The objects of its datasets waiting for an image this statement inserted: one statement, which probes
-- idx_samples_parent_id once per inserted image (by the id's digits, leading zeros trimmed, as the object's own trigger
-- reads them), so an insert with none waiting writes nothing. An object of another dataset that names the new
-- id is never written: that write would pass its dataset's guards, and a dataset being deleted would refuse this
-- insert. The ids go in as one array, so the plan doesn't depend on the insert's size: the planner has no statistics
-- on the JSON expression (it doesn't use a partial index's), and a join planned on that guess sorted every object for
-- a 500-image insert. With the array it probes the index for 1, 500 and 50,000 rows, with images or objects the most
-- of the table, planned generic or custom. Each object's own trigger (samples_set_parent) then checks its image's
-- dataset and kind.
CREATE OR REPLACE FUNCTION samples_link_new_children() RETURNS trigger AS $$
BEGIN
    UPDATE samples SET parent_id = (metadata->>'parent_id')::integer
     WHERE ltrim(metadata->>'parent_id', '0')
           = ANY (ARRAY(SELECT id::text FROM parent_new_rows WHERE media_type <> 'object'))
       AND media_type = 'object' AND parent_id IS NULL
       AND dataset_id IN (SELECT dataset_id FROM parent_new_rows WHERE media_type <> 'object');
    RETURN NULL;
END;
$$ LANGUAGE plpgsql;

-- A sample that became or stopped being an object, or moved to another dataset: its old children lose it, and the
-- objects that name it in its dataset gain it if it is an image now. Each child's own trigger re-derives the value.
CREATE OR REPLACE FUNCTION samples_relink_children() RETURNS trigger AS $$
BEGIN
    UPDATE samples SET parent_id = NULL WHERE dataset_id = OLD.dataset_id AND parent_id = OLD.id;
    IF NEW.media_type <> 'object' THEN
        UPDATE samples SET parent_id = NEW.id
         WHERE ltrim(metadata->>'parent_id', '0') = NEW.id::text AND media_type = 'object'
           AND dataset_id = NEW.dataset_id AND parent_id IS DISTINCT FROM NEW.id;
    END IF;
    RETURN NULL;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trg_samples_parent_insert
    BEFORE INSERT ON samples FOR EACH ROW WHEN (NEW.media_type = 'object' OR NEW.parent_id IS NOT NULL)
    EXECUTE FUNCTION samples_set_parent();
CREATE TRIGGER trg_samples_parent_update
    BEFORE UPDATE OF metadata, media_type, dataset_id, parent_id ON samples FOR EACH ROW
    WHEN (OLD.metadata->'parent_id' IS DISTINCT FROM NEW.metadata->'parent_id'
          OR OLD.media_type IS DISTINCT FROM NEW.media_type OR OLD.dataset_id IS DISTINCT FROM NEW.dataset_id
          OR OLD.parent_id IS DISTINCT FROM NEW.parent_id)
    EXECUTE FUNCTION samples_set_parent();
CREATE TRIGGER trg_samples_children_insert
    AFTER INSERT ON samples REFERENCING NEW TABLE AS parent_new_rows
    FOR EACH STATEMENT EXECUTE FUNCTION samples_link_new_children();
CREATE TRIGGER trg_samples_children_update
    AFTER UPDATE OF media_type, dataset_id ON samples FOR EACH ROW
    WHEN ((OLD.media_type = 'object') IS DISTINCT FROM (NEW.media_type = 'object')
          OR OLD.dataset_id IS DISTINCT FROM NEW.dataset_id)
    EXECUTE FUNCTION samples_relink_children();

-- 10. Statistics on the new column for the planner.
ANALYZE samples;
