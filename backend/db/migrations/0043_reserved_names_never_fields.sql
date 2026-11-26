-- A metadata key named id, dataset_id, filepath, created_at or last_modified_at names a sample's own field. Such keys
-- are refused wherever metadata is written; the ones stored before stay in their samples (a stored created_at can be
-- the only copy of an original date), but they are no field any more: none is registered from metadata again
-- (field_registry.infer_fields). This turns the registrations an older release made back into the sample's own
-- field, so the field list shows each name once, with the sample's own values: a stored dataset_id stops showing as
-- a field of its own, and stored strings stop going into the statistics of created_at and the others.
--
-- The rule is migration 0042's last step for tags and media_type, which is forget_definitions' for a built-in field's
-- name (field_registry.py): on a dataset that is no mirror and not being deleted, a metadata field of one of the
-- names that says something about the name (a description, a unit, the private mark that keeps its values from the
-- assistant) goes back to the sample's own field's row holding only that; any other such row goes. The registry's
-- triggers stay on, so the name is marked for the ordinary statistics refresh (counted again from the samples once
-- the app is up) and a followed dataset's change feed gets the field change, as for any field removal, and nothing
-- else. An index left over that key is named for the administrator to remove; none is dropped. A mirror's registry is
-- its primary's (its sync takes the same rule); a dataset being deleted keeps its rows until they go with it (its
-- guard refuses any write to them).
--
-- Only the field registry changes: no sample row is read for writing or written, so it needs no app stop and no
-- disk check. An older app still running meanwhile (an upgrade that skips stopping it) can register such a field
-- again by an edit, as it always did, and the field list shows it until a later upgrade's run of this file, which
-- finds it: stop the app first, as step 4 of an upgrade says. Run again, it finds nothing. Its lines start with
-- "0043:" (apply_migrations.sh shows them).
SET LOCAL lock_timeout = '2s';

DO $$
DECLARE
    field RECORD;
    leftover TEXT;
    changed BOOLEAN := FALSE;
BEGIN
    FOR field IN
        SELECT f.id, f.dataset_id, d.name, f.field_name,
               concat_ws(', ', CASE WHEN f.description IS NOT NULL THEN 'description' END,
                         CASE WHEN f.unit IS NOT NULL THEN 'unit' END,
                         CASE WHEN f.values_private THEN 'private mark' END) AS kept
          FROM dataset_fields f JOIN datasets d ON d.id = f.dataset_id
         WHERE f.field_name IN ('id', 'dataset_id', 'filepath', 'created_at', 'last_modified_at')
           AND f.origin = 'metadata' AND NOT d.deleting
           AND NOT EXISTS (SELECT 1 FROM mirrors m WHERE m.dataset_id = f.dataset_id)
         ORDER BY f.dataset_id, f.field_name
    LOOP
        -- As 0042's last step: a row that says something about the name goes back to holding only that (origin
        -- 'system', what made it a metadata field cleared); any other row goes.
        UPDATE dataset_fields SET field_type = 'unknown', origin = 'system', is_promoted = FALSE, display_type = NULL,
               overlay_config = NULL
         WHERE id = field.id AND (description IS NOT NULL OR unit IS NOT NULL OR values_private);
        DELETE FROM dataset_fields WHERE id = field.id AND origin <> 'system';
        changed := TRUE;
        -- An index over that key in this dataset (a promoted field's): left for the administrator, named here.
        SELECT string_agg(i.indexrelid::regclass::text, ', ' ORDER BY i.indexrelid::regclass::text) INTO leftover
          FROM pg_index i
         WHERE i.indrelid = 'samples'::regclass
           AND position(format('(metadata ->> %L::text)', field.field_name) IN pg_get_indexdef(i.indexrelid)) > 0
           AND position(format('WHERE (dataset_id = %s)', field.dataset_id) IN pg_get_indexdef(i.indexrelid)) > 0;
        RAISE NOTICE '0043: dataset % (%): % is no metadata field any more%: the values stay in the samples'' metadata, and the statistics of the name are counted again from the samples once the app is up%',
            field.dataset_id, field.name, field.field_name,
            CASE WHEN field.kept = '' THEN ''
                 ELSE format(' (keeping its %s for the sample''s own field)', field.kept) END,
            CASE WHEN leftover IS NULL THEN ''
                 ELSE format('. The index %s over that key is left: remove it with DROP INDEX CONCURRENTLY', leftover) END;
    END LOOP;
    IF NOT changed THEN
        RAISE NOTICE '0043: nothing to do: no dataset (but a mirror or one being deleted) has a metadata field named id, dataset_id, filepath, created_at or last_modified_at';
    END IF;
END $$;
