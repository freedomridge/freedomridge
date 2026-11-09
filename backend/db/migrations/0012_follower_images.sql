-- A primary serves a followed dataset's images to a follower when that dataset's images option is on
-- (docs/FOLLOWERS.md): for a follower that can't read them itself, from the primary's disk or a bucket it has no
-- credentials for. Off unless an admin turns it on.
SET LOCAL lock_timeout = '2s';

ALTER TABLE follower_datasets ADD COLUMN images BOOLEAN NOT NULL DEFAULT FALSE;  -- on a primary
ALTER TABLE mirrors ADD COLUMN images BOOLEAN NOT NULL DEFAULT FALSE;            -- on a follower: its primary's option
