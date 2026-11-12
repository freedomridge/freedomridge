-- Metadata datetimes as instants: an indexed datetime field's index moves from its stored text to its instant
-- (metadata_instant_v1, 0031), which is what its queries now compare. The rebuild queue 0021 made for vector indexes
-- takes field indexes too, and each indexed datetime field whose index is on its text is queued there. The app
-- rebuilds them in the background, one at a time, smallest dataset first (index_jobs.queue_rebuilds): each build
-- replaces the text index of the same name and marks the field indexed again. This file builds and drops no index.
-- Until its new index is built, the field is marked not indexed, so every query on it takes the form that needs no
-- index: results stay correct, only slower; a build that fails leaves an ordinary unindexed field.
SET LOCAL lock_timeout = '2s';

-- 2.9.0's rows are vector indexes. The key leads with the kind: the scheduler asks for each kind every 10 s.
ALTER TABLE ann_index_rebuilds ADD COLUMN kind TEXT NOT NULL DEFAULT 'vector'
    CONSTRAINT ann_index_rebuilds_kind_check CHECK (kind IN ('vector', 'field'));
ALTER TABLE ann_index_rebuilds DROP CONSTRAINT ann_index_rebuilds_pkey;
ALTER TABLE ann_index_rebuilds ADD CONSTRAINT ann_index_rebuilds_pkey PRIMARY KEY (kind, dataset_id, field_name);

-- Only is_promoted changes, and it belongs to this installation: followers never read it (each follower's own
-- migration queues its own indexes), and a mirror's guard lets it through. So the change log's trigger is off for
-- this UPDATE, on purpose (CONTRIBUTING, Database migrations, rule 4): logged, every follower of the dataset would
-- fetch its fields again for nothing.
ALTER TABLE dataset_fields DISABLE TRIGGER dataset_fields_log_update;

-- A datetime field's index on its text, found in the catalog: promote names it idx_samples_meta_<dataset>_<field>,
-- or, past 63 characters, idx_samples_meta_<dataset>_h<the first 16 hex digits of md5('<dataset>:<field>')>
-- (routers/datasets._promoted_index_name), and PostgreSQL prints its expression as (metadata ->> '<field>'::text).
WITH text_indexed AS (
    SELECT f.id, f.dataset_id, f.field_name
    FROM dataset_fields f
    JOIN pg_class c ON c.relnamespace = current_schema()::regnamespace
        AND c.relname = CASE
            WHEN length('idx_samples_meta_' || f.dataset_id || '_' || f.field_name) <= 63
                THEN 'idx_samples_meta_' || f.dataset_id || '_' || f.field_name
            ELSE 'idx_samples_meta_' || f.dataset_id || '_h' || left(md5(f.dataset_id || ':' || f.field_name), 16)
        END
    JOIN pg_index i ON i.indexrelid = c.oid AND i.indrelid = 'samples'::regclass
    WHERE f.is_promoted AND f.field_type = 'datetime'
      AND pg_get_expr(i.indexprs, i.indrelid) = format('(metadata ->> %L::text)', f.field_name)
), queued AS (
    INSERT INTO ann_index_rebuilds (kind, dataset_id, field_name)
    SELECT 'field', dataset_id, field_name FROM text_indexed
    ON CONFLICT DO NOTHING
)
UPDATE dataset_fields SET is_promoted = FALSE WHERE id IN (SELECT id FROM text_indexed);

ALTER TABLE dataset_fields ENABLE TRIGGER dataset_fields_log_update;
