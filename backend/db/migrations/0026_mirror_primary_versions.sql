-- A mirror follows its primary's dataset as it is now; the versions made on the primary stay there
-- (docs/FOLLOWERS.md). The primary says how many it keeps (GET /api/follower/config), so the mirror's Versions tab
-- can say that the dataset has a history it doesn't hold. NULL: the primary hasn't said (one from before this
-- release, or a mirror not synced since).
SET LOCAL lock_timeout = '2s';

ALTER TABLE mirrors ADD COLUMN primary_versions INTEGER;
