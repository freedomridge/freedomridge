-- A new copy of a dataset a follower copied before resumes into its stopped mirror (docs/FOLLOWERS.md), keeping
-- what this instance added there, when the primary is the same database: so a mirror remembers its primary's
-- identity (the link row can go), when it stopped, and whether it was kept as a separate copy, never resumed.
SET LOCAL lock_timeout = '2s';

ALTER TABLE mirrors ADD COLUMN primary_identity TEXT;
ALTER TABLE mirrors ADD COLUMN stopped_at TIMESTAMP WITH TIME ZONE;
ALTER TABLE mirrors ADD COLUMN keep_separate BOOLEAN NOT NULL DEFAULT FALSE;
-- Only copies still copying or following get their link's identity. A stopped one may have stopped in an older
-- database: after a restore, the link holds the new database's identity. So copies stopped before this migration
-- are never resumed.
UPDATE mirrors m SET primary_identity = l.identity FROM primary_links l WHERE l.id = m.link_id AND m.state <> 'stopped';
