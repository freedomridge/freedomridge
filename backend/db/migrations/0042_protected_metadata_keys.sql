-- A metadata key named tags or media_type names a sample's own field: everything that says tags or media_type reads
-- the field, so the key was never listed, filtered or shown, and since this release it is refused wherever metadata is
-- written (field_registry.PROTECTED_METADATA_KEYS). This removes the stored ones, for good: the values are gone (the
-- upgrade notes say how to keep them first). Everything else about a sample stays as it was: its other keys, its own
-- tags and media_type, its last_modified_at, its vectors.
--
-- Only the rows that carry a key are read and written (a row without one keeps its row version), found through
-- idx_samples_metadata (a GIN index over every key, which answers `?|`); on an instance that dropped it, by one read of
-- the samples table. Only a metadata that is an object counts: on a list `?|` and `-` would act on its string elements,
-- and a string or JSON null has no keys (the app refuses all three; an old database may hold one). With nothing to
-- remove it changes no row and takes no lock beyond that read. Otherwise it runs like 0029, with the app stopped (step
-- 4 of an upgrade): it refuses while anything else is connected, checks the disk, and removes the keys in this one
-- transaction with the samples table's triggers off, on purpose (CONTRIBUTING, Database migrations, rule 4): a key is
-- no field (step 5 has the fields), so there is nothing to tell followers, the statistics, a field index or the
-- duplicate work, and on a follower the mirror guard would refuse the change. A mirror's rows lose the keys too, as
-- its primary's do when it upgrades; whatever its primary sends later, it stores as before. Afterwards no sample may
-- carry a key: one written meanwhile fails the migration. A failure or an interruption rolls everything back, and it
-- runs again from the start; run again after it, it finds nothing. Its lines start with "0042:" (apply_migrations.sh
-- shows them). Afterwards each changed sample leaves a dead row version for VACUUM. A dataset version keeps its rows as
-- they were saved: a revert leaves the two keys out (routers/versions.py).
--
-- A field registered under one of the names (a rename or a clone onto it, which earlier releases allowed) goes too, in
-- step 5, with the field registry's triggers on: followers hear of it as of any field removal, and the statistics of
-- the sample's own field of that name are counted again from the samples once the app is up (the statistics job, the
-- one piece of work this needs). What was said about the name stays, as the app's forget_definitions keeps it for a
-- built-in field: a rename onto it landed on the built-in field's own row, so its description, unit and private mark
-- (the assistant doesn't get its values) are the sample's own field's. An index left over that key is named for the
-- administrator to remove. A mirror's registry is its primary's and stays as it is; so does a dataset being deleted,
-- whose rows go with it (its guard refuses any write). Step 5 writes even with the app up, as it changes no sample: an
-- older app still running (an upgrade that skips stopping it) could register such a field again after it, by a rename
-- started meanwhile, so stop the app first, as step 4 of an upgrade says.
SET LOCAL lock_timeout = '2s';

-- 1. The samples carrying a key, with which key, by one read of the samples table (the index's, where it is there).
CREATE TEMP TABLE _fridge_keyed ON COMMIT DROP AS
    SELECT id, dataset_id, metadata ? 'tags' AS has_tags, metadata ? 'media_type' AS has_media_type
      FROM samples WHERE metadata ?| ARRAY['tags', 'media_type'] AND jsonb_typeof(metadata) = 'object';

DO $$
DECLARE
    keyed BIGINT := (SELECT count(*) FROM _fridge_keyed);
    others TEXT;
    rows_now BIGINT;
    written BIGINT;
    wal BIGINT;
    needed BIGINT;
    free BIGINT;
    data_dir TEXT;
BEGIN
    IF keyed = 0 THEN
        RETURN;
    END IF;
    -- 2. Nothing else may be connected: the triggers are off until the commit, and a write meanwhile would wait. A role
    -- that is no superuser and lacks pg_read_all_stats sees another role's session without its backend_type, so a
    -- session without one counts too (background processes have no user).
    SELECT string_agg(DISTINCT coalesce(nullif(application_name, ''), 'a client') || ' (' || usename || ')', ', ')
      INTO others
      FROM pg_stat_activity
     WHERE datname = current_database() AND pid <> pg_backend_pid()
       AND (backend_type = 'client backend' OR backend_type IS NULL) AND usename IS NOT NULL;
    IF others IS NOT NULL THEN
        RAISE EXCEPTION '0042 removes the metadata keys tags and media_type from % samples, so stop the app first. Still connected: %',
            keyed, others
            USING HINT = 'Stop every service but the database (backend, job runner, workers, backups), then run '
                         'apply_migrations.sh again.';
    END IF;
    -- 3. Room for a new version of each of those rows (heap, TOAST and index entries, at the table's average), the WAL
    -- written meanwhile and the headroom 0029 keeps. The WAL is mostly whole pages: one for each page of the table and
    -- of each of its indexes that a changed row touches, which for keys spread through a dataset is nearly all of them
    -- (measured: 0.9 GB of WAL for 121,000 such rows among 1.1 million, about the table's size with its indexes). It
    -- levels off at max_wal_size, which is soft; WAL held back by a replication slot or a failing archive_command is not
    -- counted. On a table never analysed (reltuples -1, as just after a restore) the estimate is the whole table's,
    -- more than needed. Skipped where the server can't run df or df gives no number, as the app's own check is.
    rows_now := greatest((SELECT reltuples FROM pg_class WHERE oid = 'samples'::regclass)::BIGINT, keyed);
    written := (keyed::NUMERIC * pg_total_relation_size('samples') / rows_now)::BIGINT;
    wal := least(
        least(keyed::NUMERIC * 8192 * (1 + (SELECT count(*) FROM pg_index WHERE indrelid = 'samples'::regclass)),
              pg_total_relation_size('samples')) + written,
        pg_size_bytes(current_setting('max_wal_size')))::BIGINT;
    needed := written + wal
        + greatest(written / 4, least(greatest(written, pg_size_bytes('64MB')), pg_size_bytes('1GB')));
    BEGIN
        data_dir := current_setting('data_directory');
        IF position('''' IN data_dir) > 0 THEN
            RAISE NOTICE '0042: skipped the free-space check (the data directory''s name has a quote)';
            RETURN;
        END IF;
        CREATE TEMP TABLE _fridge_df (line TEXT) ON COMMIT DROP;
        EXECUTE format('COPY _fridge_df FROM PROGRAM %L', 'df -B1 --output=avail ''' || data_dir || ''' | tail -n 1');
        SELECT trim(line)::BIGINT INTO free FROM _fridge_df LIMIT 1;
    EXCEPTION WHEN OTHERS THEN
        RAISE NOTICE '0042: skipped the free-space check: %', SQLERRM;
        RETURN;
    END;
    IF free IS NULL THEN
        RAISE NOTICE '0042: skipped the free-space check (df gave no number)';
        RETURN;
    END IF;
    IF free < needed THEN
        RAISE EXCEPTION '0042 needs about % free on the database''s disk to remove the keys from % samples; % is free',
            pg_size_pretty(needed), keyed, pg_size_pretty(free)
            USING HINT = 'Free some space, or move the database to a larger disk, then run apply_migrations.sh again.';
    END IF;
    RAISE NOTICE '0042: % free on the database''s disk, about % needed', pg_size_pretty(free), pg_size_pretty(needed);
END $$;

-- 4. The keys removed, with the triggers off (above), and what was done said for each dataset.
DO $$
DECLARE
    dataset RECORD;
    changed BIGINT;
BEGIN
    IF NOT EXISTS (SELECT 1 FROM _fridge_keyed) THEN
        RETURN;
    END IF;
    ALTER TABLE samples DISABLE TRIGGER USER;
    UPDATE samples s SET metadata = s.metadata - ARRAY['tags', 'media_type'] FROM _fridge_keyed k WHERE s.id = k.id;
    GET DIAGNOSTICS changed = ROW_COUNT;
    ALTER TABLE samples ENABLE TRIGGER USER;
    -- None may be left: a key written after the read above (by something still connected that the check missed)
    -- fails the migration here, changing nothing, instead of staying.
    IF EXISTS (SELECT 1 FROM samples WHERE metadata ?| ARRAY['tags', 'media_type'] AND jsonb_typeof(metadata) = 'object')
    THEN
        RAISE EXCEPTION '0042 found a sample still carrying a metadata key named tags or media_type: one was written meanwhile'
            USING HINT = 'Stop every service but the database, then run apply_migrations.sh again.';
    END IF;
    -- One line for each dataset, naming each key it removed and from how many samples.
    FOR dataset IN
        SELECT d.id, d.name, EXISTS (SELECT 1 FROM mirrors m WHERE m.dataset_id = d.id) AS mirror,
               count(*) FILTER (WHERE k.has_tags) AS tags, count(*) FILTER (WHERE k.has_media_type) AS media_types
          FROM _fridge_keyed k JOIN datasets d ON d.id = k.dataset_id
         GROUP BY d.id ORDER BY d.id
    LOOP
        RAISE NOTICE '0042: dataset % (%)%: removed %', dataset.id, dataset.name,
            CASE WHEN dataset.mirror THEN ', a mirror' ELSE '' END,
            concat_ws(', ',
                CASE dataset.tags WHEN 0 THEN NULL WHEN 1 THEN 'tags from 1 sample'
                     ELSE format('tags from %s samples', dataset.tags) END,
                CASE dataset.media_types WHEN 0 THEN NULL WHEN 1 THEN 'media_type from 1 sample'
                     ELSE format('media_type from %s samples', dataset.media_types) END);
    END LOOP;
    RAISE NOTICE '0042: % changed in all; run VACUUM (ANALYZE) samples once the app is up',
        CASE WHEN changed = 1 THEN '1 sample' ELSE changed || ' samples' END;
END $$;

-- 5. A metadata field registered as tags or media_type, on a dataset that is no mirror and not being deleted, removed
-- with the registry's triggers on (above): it stood in the field list in place of the sample's own field, and nothing
-- removes it later (pruning leaves those names alone). Run whether or not a sample still carried the key.
DO $$
DECLARE
    field RECORD;
    leftover TEXT;
    removed BOOLEAN := FALSE;
BEGIN
    FOR field IN
        SELECT f.id, f.dataset_id, d.name, f.field_name,
               concat_ws(', ', CASE WHEN f.description IS NOT NULL THEN 'description' END,
                         CASE WHEN f.unit IS NOT NULL THEN 'unit' END,
                         CASE WHEN f.values_private THEN 'private mark' END) AS kept
          FROM dataset_fields f JOIN datasets d ON d.id = f.dataset_id
         WHERE f.field_name IN ('tags', 'media_type') AND f.origin = 'metadata' AND NOT d.deleting
           AND NOT EXISTS (SELECT 1 FROM mirrors m WHERE m.dataset_id = f.dataset_id)
         ORDER BY f.dataset_id, f.field_name
    LOOP
        -- As forget_definitions (field_registry.py) for a built-in field's name: a row that says something about the
        -- name goes back to holding only that (origin 'system', what made it a metadata field cleared); any other
        -- row goes.
        UPDATE dataset_fields SET field_type = 'unknown', origin = 'system', is_promoted = FALSE, display_type = NULL,
               overlay_config = NULL
         WHERE id = field.id AND (description IS NOT NULL OR unit IS NOT NULL OR values_private);
        DELETE FROM dataset_fields WHERE id = field.id AND origin <> 'system';
        removed := TRUE;
        -- An index over that key in this dataset (a promoted field's): left for the administrator, named here.
        SELECT string_agg(i.indexrelid::regclass::text, ', ' ORDER BY i.indexrelid::regclass::text) INTO leftover
          FROM pg_index i
         WHERE i.indrelid = 'samples'::regclass
           AND position(format('(metadata ->> %L::text)', field.field_name) IN pg_get_indexdef(i.indexrelid)) > 0
           AND position(format('WHERE (dataset_id = %s)', field.dataset_id) IN pg_get_indexdef(i.indexrelid)) > 0;
        RAISE NOTICE '0042: dataset % (%): removed the metadata field %, which stood in place of the sample''s own%: its statistics are counted again from the samples once the app is up%',
            field.dataset_id, field.name, field.field_name,
            CASE WHEN field.kept = '' THEN ''
                 ELSE format(' (keeping its %s for the sample''s own field)', field.kept) END,
            CASE WHEN leftover IS NULL THEN ''
                 ELSE format('. The index %s over that key is left: remove it with DROP INDEX CONCURRENTLY', leftover) END;
    END LOOP;
    IF NOT removed AND NOT EXISTS (SELECT 1 FROM _fridge_keyed) THEN
        RAISE NOTICE '0042: nothing to remove: no sample''s metadata has a key named tags or media_type, and no dataset (but a mirror or one being deleted) has a metadata field of either name';
    END IF;
END $$;
