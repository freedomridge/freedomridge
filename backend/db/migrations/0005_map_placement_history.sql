-- Map placement (kind map_place, backend/map_placement.py) runs as often as new samples arrive on a re-fitted
-- map. Like the other automatic maintenance, its successful runs stay out of a dataset's job history and are
-- deleted after a day: both partial indexes of that history take the new kind. They are built without
-- CONCURRENTLY (a migration is one transaction), so job writes wait for the builds: the jobs table is small,
-- and old automatic runs are pruned daily.
SET LOCAL lock_timeout = '2s';

DROP INDEX idx_jobs_dataset_history;
DROP INDEX idx_statistics_history_retention;
CREATE INDEX idx_jobs_dataset_history ON jobs(dataset_id,created_at DESC,id DESC)
    WHERE NOT (kind IN ('statistics','concept_scoring','duplicate_maintenance','map_place')
               AND dedupe_owner='system' AND state='succeeded' AND NOT barrier @> '{"full":true}');
CREATE INDEX idx_statistics_history_retention ON jobs(finished_at,id)
    WHERE kind IN ('statistics','concept_scoring','duplicate_maintenance','map_place')
          AND dedupe_owner='system' AND state='succeeded' AND NOT barrier @> '{"full":true}';
