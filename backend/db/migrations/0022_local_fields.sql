-- A mirror's own fields (docs/FOLLOWERS.md): a follower may add labels, metadata fields and tags of its own beside
-- the primary's, addressed as `@local.<name>`, and never change the primary's. They're stored in two columns the
-- sync never writes, so every re-read and resume keeps them. dataset_fields says who owns each field.
SET LOCAL lock_timeout = '2s';

-- Two things in the way, refused with their names: an array field already called local_metadata or local_tags, and
-- a metadata key already registered under a name starting with `@local.` (field discovery took any key until now).
DO $$
DECLARE
    taken TEXT;
BEGIN
    SELECT string_agg(attname::text, ', ') INTO taken FROM pg_attribute
    WHERE attrelid = 'samples'::regclass AND attname IN ('local_metadata', 'local_tags') AND NOT attisdropped;
    IF taken IS NOT NULL THEN
        RAISE EXCEPTION 'samples already has a column named % (an array field): this release needs the name for a mirror''s own fields. Copy that field to another name and delete it, then upgrade', taken;
    END IF;
    SELECT string_agg(format('%s (dataset %s)', field_name, dataset_id), ', ') INTO taken FROM dataset_fields
    WHERE starts_with(field_name, '@local.');
    IF taken IS NOT NULL THEN
        RAISE EXCEPTION 'Metadata fields named % start with @local., which this release keeps for a mirror''s own fields. Rename those keys in the samples'' metadata and delete the fields, then upgrade', taken;
    END IF;
END;
$$;

-- `@local.weeds` is local_metadata->'weeds'; `@local.tags` is local_tags. Constant defaults: no table rewrite.
ALTER TABLE samples ADD COLUMN local_metadata JSONB NOT NULL DEFAULT '{}'::jsonb;
ALTER TABLE samples ADD COLUMN local_tags TEXT[] DEFAULT '{}';

-- origin is where a field is stored, owner who may change it: 'primary' on a mirror for the primary's fields, 'self'
-- for this instance's. The `@local.` names are always origin 'local', and only they are.
ALTER TABLE dataset_fields DROP CONSTRAINT dataset_fields_origin_check;
ALTER TABLE dataset_fields ADD CONSTRAINT dataset_fields_origin_check
    CHECK (origin IN ('metadata', 'native_array', 'system', 'local'));
ALTER TABLE dataset_fields ADD CONSTRAINT dataset_fields_local_check
    CHECK (starts_with(field_name, '@local.') = (origin = 'local'));
ALTER TABLE dataset_fields ADD COLUMN owner TEXT NOT NULL DEFAULT 'self'
    CONSTRAINT dataset_fields_owner_check CHECK (owner IN ('self', 'primary'));

-- Fields are registered in several places (discovery, the sync, bulk jobs, versions, promote, create), so none of
-- them decides the owner: on a mirror, every field but this instance's own `@local.` ones is the primary's.
CREATE OR REPLACE FUNCTION set_field_owner() RETURNS trigger AS $$
BEGIN
    NEW.owner := CASE
        WHEN NOT starts_with(NEW.field_name, '@local.')
             AND EXISTS (SELECT 1 FROM mirrors WHERE dataset_id = NEW.dataset_id) THEN 'primary'
        ELSE 'self'
    END;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER dataset_fields_owner BEFORE INSERT OR UPDATE ON dataset_fields
    FOR EACH ROW EXECUTE FUNCTION set_field_owner();

-- Every field of a mirror so far came from its primary. The trigger above sets it; the mirror guard lets an update of
-- owner alone through.
UPDATE dataset_fields SET owner = 'primary' WHERE dataset_id IN (SELECT dataset_id FROM mirrors);

-- The field registry of a mirror is the primary's, except this instance's own `@local.` fields.
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
    IF TG_OP <> 'INSERT' AND NOT starts_with(OLD.field_name, '@local.') THEN
        PERFORM require_mirror_sync(OLD.dataset_id);
    END IF;
    IF TG_OP <> 'DELETE' AND NOT starts_with(NEW.field_name, '@local.') THEN
        PERFORM require_mirror_sync(NEW.dataset_id);
    END IF;
    RETURN NULL;
END;
$$ LANGUAGE plpgsql;

-- A followed dataset's changed sample block (0007); a mirror's own fields may change too.
CREATE OR REPLACE FUNCTION sample_block_changed(target_dataset INTEGER, target_block BIGINT) RETURNS VOID AS $$
BEGIN
    IF current_setting('fridge.deletion_dataset', TRUE) = target_dataset::text THEN
        RETURN;
    END IF;
    -- A mirror's samples change only through its sync (0009), or, for this instance's own fields, under
    -- fridge.mirror_local, which samples_mirror_local_guard holds to the two local columns (0022).
    IF current_setting('fridge.mirror_local', TRUE) IS DISTINCT FROM target_dataset::text THEN
        PERFORM require_mirror_sync(target_dataset);
    END IF;
    INSERT INTO sample_block_changes (dataset_id, block_id)
    SELECT target_dataset, target_block FROM datasets WHERE id = target_dataset AND followed
    ON CONFLICT DO NOTHING;
END;
$$ LANGUAGE plpgsql;

-- Under fridge.mirror_local (a dataset id), a write may change local_metadata and local_tags of that dataset's
-- samples and nothing else: no insert, no delete, no other column, including array columns added later. AFTER, so it
-- sees each row as stored, whatever a BEFORE trigger changed.
CREATE OR REPLACE FUNCTION guard_mirror_local() RETURNS trigger AS $$
BEGIN
    IF TG_OP <> 'UPDATE'
       OR NEW.dataset_id::text IS DISTINCT FROM current_setting('fridge.mirror_local', TRUE)
       OR to_jsonb(OLD) - 'local_metadata' - 'local_tags' IS DISTINCT FROM to_jsonb(NEW) - 'local_metadata' - 'local_tags'
    THEN
        RAISE EXCEPTION 'A write of this instance''s own fields (@local.) changes only those fields'
            USING ERRCODE = '55000';
    END IF;
    RETURN NULL;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER samples_mirror_local_guard AFTER INSERT OR UPDATE OR DELETE ON samples
    FOR EACH ROW WHEN (NULLIF(current_setting('fridge.mirror_local', TRUE), '') IS NOT NULL)
    EXECUTE FUNCTION guard_mirror_local();
