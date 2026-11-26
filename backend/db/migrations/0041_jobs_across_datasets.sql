-- GET /api/admin/jobs/all (backend/routers/jobs.py) lists the jobs of the whole server by state, newest first, within a
-- window of time, with counts by state and kind. idx_jobs_state_created serves each state's range of it as an
-- index-only scan, kind and dataset included; idx_jobs_named_dataset finds the jobs of no dataset that name one in their
-- arguments (a dataset's index reviews), which a dataset's own indexes don't hold. Both are built without CONCURRENTLY
-- (a migration is one transaction), so job writes wait for the builds: about a second for a million jobs.
SET LOCAL lock_timeout = '2s';

CREATE INDEX idx_jobs_state_created ON jobs (state, created_at DESC, id DESC) INCLUDE (kind, dataset_id);
CREATE INDEX idx_jobs_named_dataset ON jobs ((args->>'dataset_id'), created_at DESC, id DESC)
    WHERE dataset_id IS NULL AND args ? 'dataset_id';
