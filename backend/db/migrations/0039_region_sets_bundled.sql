-- region_sets.loaded_from also accepts 'bundled': a set loaded from the pinned file that came with the install
-- (docs/REGIONS.md, "Loading a set"), by the job runner on its own or by an administrator's load. Nothing else
-- changes; region_sets holds one row per loaded set.
SET LOCAL lock_timeout = '2s';

ALTER TABLE region_sets DROP CONSTRAINT region_sets_loaded_from_check;
ALTER TABLE region_sets ADD CONSTRAINT region_sets_loaded_from_check
    CHECK (loaded_from IN ('download', 'file', 'bundled'));
